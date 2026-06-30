"""LLM Judge — adapter config-driven via API Gateway, com cassette record/replay.

- A request é montada inteiramente a partir de ``config/judge.yaml`` (url, method,
  headers, body_template) — o contrato exato do gateway é trocável sem mexer no código.
- Determinismo: em modo **replay** (CI) as respostas vêm de um cassette gravado; em
  modo **live** (``--live``) chama o gateway real e regrava o cassette.
- A extração de score/verdict usa caminhos pontilhados configuráveis.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Optional

import httpx

from orchestrator.graph.state import JudgeVerdict

# Critério fixo de QC (Step 7 do Context.md): realista, detalhe limpo, passa no "real test".
DEFAULT_QC_CRITERIA = {
    "realistic": "parece uma pessoa real?",
    "detail_clean": "mãos/olhos/lip-sync/iluminação sem artefato?",
    "real_test": "passa como pessoa real nos 2 primeiros segundos sem contexto?",
}


class CassetteMiss(KeyError):
    """Não há gravação para a chave pedida (rode com --live para gravar)."""


class Cassette:
    """Armazena respostas do gateway num JSON, chaveadas por id lógico do subject."""

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self.data: dict[str, Any] = (
            json.loads(self.path.read_text(encoding="utf-8")) if self.path.exists() else {}
        )

    def play(self, key: str) -> Optional[dict[str, Any]]:
        return self.data.get(key)

    def record(self, key: str, status: int, body: dict[str, Any]) -> None:
        self.data[key] = {"status": status, "json": body}
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(
            json.dumps(self.data, indent=2, sort_keys=True, ensure_ascii=False), encoding="utf-8"
        )


def dig(obj: Any, dotted: str) -> Any:
    """Extrai um valor por caminho pontilhado 'a.b.c'."""
    cur = obj
    for part in dotted.split("."):
        cur = cur[part]
    return cur


class GatewayJudge:
    """Implementa o JudgePort chamando um API Gateway descrito por config."""

    def __init__(
        self,
        config: dict[str, Any],
        cassette: Optional[Cassette] = None,
        live: bool = False,
        client: Optional[httpx.Client] = None,
    ) -> None:
        self.gw = config["gateway"]
        self.body_template = config["body_template"]
        self.resp_cfg = config["response"]
        self.cassette = cassette
        self.live = live
        self._client = client

    def build_request(self, criteria: dict[str, Any], subject: dict[str, Any]) -> dict[str, Any]:
        # template é JSON com chaves literais -> substitui só os placeholders (sem str.format)
        body = (
            self.body_template
            .replace("{criteria_json}", json.dumps(criteria, ensure_ascii=False))
            .replace("{subject_json}", json.dumps(subject, ensure_ascii=False))
        )
        return {
            "method": self.gw.get("method", "POST"),
            "url": self.gw["url"],
            "headers": self.gw.get("headers", {}),
            "content": body,
        }

    def _send(self, req: dict[str, Any]) -> tuple[int, dict[str, Any]]:
        client = self._client or httpx.Client(timeout=float(self.gw.get("timeout_seconds", 30)))
        try:
            resp = client.request(
                req["method"], req["url"], headers=req["headers"], content=req["content"]
            )
            return resp.status_code, resp.json()
        finally:
            if self._client is None:
                client.close()

    def judge(
        self,
        criteria: dict[str, Any],
        subject: dict[str, Any],
        key: Optional[str] = None,
    ) -> JudgeVerdict:
        key = key or str(subject.get("id", ""))
        if not key:
            raise ValueError("subject precisa de 'id' (ou passe key=) para o cassette")
        req = self.build_request(criteria, subject)

        if self.live:
            status, data = self._send(req)
            if self.cassette is not None:
                self.cassette.record(key, status, data)
        else:
            rec = self.cassette.play(key) if self.cassette is not None else None
            if rec is None:
                raise CassetteMiss(f"sem gravação para {key!r}; rode com --live para gravar")
            status, data = rec["status"], rec["json"]

        score = float(dig(data, self.resp_cfg["score_path"]))
        verdict = None
        vpath = self.resp_cfg.get("verdict_path")
        if vpath:
            try:
                verdict = dig(data, vpath)
            except (KeyError, TypeError):
                verdict = None
        threshold = float(self.resp_cfg.get("pass_threshold", 0.8))
        return JudgeVerdict.from_response(score, verdict, threshold, raw=data)


# ---------------- Avaliação (estilo LangSmith) ----------------

# Critério de aderência ao escopo (offer + system prompts)
SCOPE_CRITERIA = {
    "on_offer": "o conteúdo trata do produto/oferta pedido?",
    "on_prompt": "respeita o system prompt (persona/estilo/restrições)?",
    "no_offtopic": "evita introduzir tema fora do escopo?",
}


def qc_correctness_evaluator(verdict: JudgeVerdict, expected_pass: bool) -> dict[str, Any]:
    """Evaluator: o veredito do judge bate com o rótulo humano?"""
    correct = verdict.passed == expected_pass
    return {"key": "qc_correctness", "score": 1.0 if correct else 0.0}


def scope_adherence_evaluator(verdict: JudgeVerdict, expected_pass: bool) -> dict[str, Any]:
    """Evaluator: o veredito de aderência ao escopo bate com o rótulo?"""
    correct = verdict.passed == expected_pass
    return {"key": "scope_adherence", "score": 1.0 if correct else 0.0}


def evaluate_judge(
    judge: GatewayJudge,
    dataset: list[dict[str, Any]],
    criteria: Optional[dict[str, Any]] = None,
    evaluator: Any = None,
) -> dict[str, Any]:
    """Roda o judge sobre o dataset e agrega a acurácia vs rótulos esperados.

    Retrocompatível: sem ``criteria`` usa ``DEFAULT_QC_CRITERIA``; sem ``evaluator``
    usa ``qc_correctness_evaluator``.
    """
    criteria = criteria or DEFAULT_QC_CRITERIA
    if evaluator is None:
        evaluator = qc_correctness_evaluator
    rows = []
    for ex in dataset:
        verdict = judge.judge(criteria, ex["subject"], key=ex["id"])
        ev = evaluator(verdict, ex["expected_pass"])
        rows.append(
            {"id": ex["id"], "score": verdict.score, "passed": verdict.passed, **ev}
        )
    accuracy = sum(r["score"] for r in rows) / len(rows) if rows else 0.0
    return {"accuracy": accuracy, "n": len(rows), "rows": rows}
