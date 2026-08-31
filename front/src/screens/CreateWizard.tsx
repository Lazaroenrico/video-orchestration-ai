import { useMemo, useState } from "react";
import { useNavigate } from "react-router";
import { Icon } from "../components/Icon";
import { Button } from "../components/Button";
import { Card } from "../components/Card";
import { useSessionQuery, useStartRunV2Mutation } from "../api/queries";
import type { PerformanceSnapshot } from "../types";

const PLATFORMS = ["tiktok", "instagram", "youtube", "facebook"] as const;
const OBJECTIVES = ["conversion", "awareness", "consideration"] as const;
const STEPS = ["Briefing", "Direção", "Revisão"];

const label =
  "mb-1 block font-label-sm text-label-sm uppercase text-on-surface-variant";
const field = "hm-field";

function optional(value: string): string | null {
  return value.trim() || null;
}

export function CreateWizard() {
  const navigate = useNavigate();
  const startRun = useStartRunV2Mutation();
  const session = useSessionQuery();
  const canCreate = session.data ? session.data.permissions.includes("runs:create") : false;
  const [step, setStep] = useState(0);
  const [error, setError] = useState<string | null>(null);

  const [offer, setOffer] = useState("");
  const [audience, setAudience] = useState("");
  const [factsRestrictions, setFactsRestrictions] = useState("");
  const [creatorDirection, setCreatorDirection] = useState("");
  const [videoDirection, setVideoDirection] = useState("");
  const [performanceJson, setPerformanceJson] = useState("");
  const [batch, setBatch] = useState(6);
  const [platform, setPlatform] = useState<(typeof PLATFORMS)[number]>("tiktok");
  const [objective, setObjective] =
    useState<(typeof OBJECTIVES)[number]>("conversion");

  const performance = useMemo(() => {
    if (!performanceJson.trim()) return { value: null, error: null };
    try {
      return {
        value: JSON.parse(performanceJson) as PerformanceSnapshot,
        error: null,
      };
    } catch {
      return { value: null, error: "O JSON de performance não é válido." };
    }
  }, [performanceJson]);

  const canContinue =
    step === 0
      ? Boolean(offer.trim() && audience.trim())
      : step === 1
        ? performance.error === null
        : true;

  async function launch() {
    setError(null);
    if (performance.error) {
      setError(performance.error);
      return;
    }
    try {
      const { run_id } = await startRun.mutateAsync({
        campaign: {
          offer: offer.trim(),
          audience: audience.trim(),
          facts_restrictions: optional(factsRestrictions),
          creator_direction: optional(creatorDirection),
          video_direction: optional(videoDirection),
          platform,
          objective,
          batch_size: batch,
          performance: performance.value,
        },
      });
      navigate(`/campaigns/${run_id}`);
    } catch (caught) {
      setError(caught instanceof Error ? caught.message : String(caught));
    }
  }

  if (!canCreate) {
    return (
      <div className="min-h-[100dvh] bg-background px-4 py-6 sm:px-6 lg:px-margin-desktop lg:py-10">
        <div className="mx-auto max-w-3xl">
          <Card>
            <h1 className="hm-page-title mb-2 text-primary">Nova campanha</h1>
            <p className="font-body-md text-body-md text-on-surface-variant">
              A criação de novas campanhas é restrita a membros com permissão de criação.
            </p>
            <div className="mt-4">
              <Button variant="secondary" onClick={() => navigate("/")}>
                Voltar ao Dashboard
              </Button>
            </div>
          </Card>
        </div>
      </div>
    );
  }

  return (
    <div className="min-h-[100dvh] bg-background px-4 py-6 sm:px-6 lg:px-margin-desktop lg:py-10">
      <div className="mx-auto max-w-3xl">
        <div className="mb-gutter flex items-start justify-between gap-4">
          <div className="min-w-0">
            <h1 className="hm-page-title mb-1 text-primary">Nova campanha</h1>
            <p className="font-body-md text-body-md text-on-surface-variant">
              Informe o contexto comercial. Conceitos, roteiros e dois creators serão preparados para uma única revisão.
            </p>
          </div>
          <button
            type="button"
            onClick={() => navigate("/")}
            className="inline-flex min-h-11 items-center gap-2 whitespace-nowrap rounded-lg px-2 font-label-md text-label-md text-on-surface-variant hover:text-primary"
            aria-label="Sair da configuração"
          >
            <Icon name="close" size={18} />
          </button>
        </div>

        <ol className="mb-8 grid grid-cols-3 gap-2" aria-label="Etapas da configuração">
          {STEPS.map((name, index) => (
            <li key={name} className="min-w-0">
              <div
                className={`flex min-h-11 items-center gap-2 rounded-lg px-2 font-label-sm text-label-sm font-bold ${
                  index <= step
                    ? "bg-primary text-on-primary"
                    : "bg-surface-container-high text-on-surface-variant"
                }`}
                aria-current={index === step ? "step" : undefined}
              >
                <span className="flex size-6 shrink-0 items-center justify-center rounded-full bg-surface-container-lowest text-primary">
                  {index < step ? <Icon name="check" size={16} /> : index + 1}
                </span>
                <span className="truncate">{name}</span>
              </div>
            </li>
          ))}
        </ol>

        <Card>
          {step === 0 && (
            <div className="flex flex-col gap-5">
              <div>
                <label className={label} htmlFor="offer">Produto e oferta *</label>
                <textarea
                  id="offer"
                  className={field}
                  rows={4}
                  placeholder="O que está sendo vendido, preço ou condição da oferta e benefício principal comprovado."
                  value={offer}
                  onChange={(event) => setOffer(event.target.value)}
                  aria-required="true"
                />
              </div>
              <div>
                <label className={label} htmlFor="audience">Público *</label>
                <textarea
                  id="audience"
                  className={field}
                  rows={4}
                  placeholder="Quem deve comprar, qual problema enfrenta e em qual situação usaria o produto."
                  value={audience}
                  onChange={(event) => setAudience(event.target.value)}
                  aria-required="true"
                />
              </div>
              <div>
                <label className={label} htmlFor="facts">Fatos e restrições</label>
                <textarea
                  id="facts"
                  className={field}
                  rows={4}
                  placeholder="Fatos comprovados, claims permitidos, requisitos legais e assuntos que não podem aparecer."
                  value={factsRestrictions}
                  onChange={(event) => setFactsRestrictions(event.target.value)}
                />
              </div>
            </div>
          )}

          {step === 1 && (
            <div className="flex flex-col gap-5">
              <div>
                <label className={label} htmlFor="creator-direction">Direção dos creators</label>
                <textarea
                  id="creator-direction"
                  className={field}
                  rows={4}
                  placeholder="Perfil, aparência, voz, energia, figurino ou cenário desejado."
                  value={creatorDirection}
                  onChange={(event) => setCreatorDirection(event.target.value)}
                />
              </div>
              <div>
                <label className={label} htmlFor="video-direction">Direção dos vídeos</label>
                <textarea
                  id="video-direction"
                  className={field}
                  rows={4}
                  placeholder="Demonstração, enquadramento, ritmo, cenário e referências visuais."
                  value={videoDirection}
                  onChange={(event) => setVideoDirection(event.target.value)}
                />
              </div>
              <div>
                <label className={label} htmlFor="performance">Performance anterior em JSON</label>
                <textarea
                  id="performance"
                  className={field}
                  rows={5}
                  placeholder={'{"metrics":[{"creative_id":"ad-01","impressions":1000,"clicks":80,"conversions":9,"spend_usd":42}]}'}
                  value={performanceJson}
                  onChange={(event) => setPerformanceJson(event.target.value)}
                  aria-invalid={Boolean(performance.error)}
                />
                {performance.error && (
                  <p role="alert" className="mt-2 font-label-sm text-label-sm text-error">
                    {performance.error}
                  </p>
                )}
              </div>
              <div className="grid gap-4 sm:grid-cols-3">
                <div>
                  <label className={label} htmlFor="platform">Plataforma</label>
                  <select
                    id="platform"
                    className={field}
                    value={platform}
                    onChange={(event) =>
                      setPlatform(event.target.value as (typeof PLATFORMS)[number])
                    }
                  >
                    {PLATFORMS.map((value) => <option key={value}>{value}</option>)}
                  </select>
                </div>
                <div>
                  <label className={label} htmlFor="objective">Objetivo</label>
                  <select
                    id="objective"
                    className={field}
                    value={objective}
                    onChange={(event) =>
                      setObjective(event.target.value as (typeof OBJECTIVES)[number])
                    }
                  >
                    {OBJECTIVES.map((value) => <option key={value}>{value}</option>)}
                  </select>
                </div>
                <div>
                  <label className={label} htmlFor="batch-size">Pacotes</label>
                  <input
                    id="batch-size"
                    type="number"
                    min={1}
                    max={48}
                    className={field}
                    value={batch}
                    onChange={(event) =>
                      setBatch(Math.min(48, Math.max(1, Number(event.target.value) || 1)))
                    }
                  />
                </div>
              </div>
            </div>
          )}

          {step === 2 && (
            <div className="flex flex-col gap-3 font-body-md text-body-md">
              <div className="mb-2 flex items-center gap-2 text-primary">
                <Icon name="fact_check" />
                <span className="font-headline-md text-headline-md">Confirmar execução</span>
              </div>
              {[
                ["Produto e oferta", offer],
                ["Público", audience],
                ["Plataforma", platform],
                ["Objetivo", objective],
                ["Pacotes criativos", String(batch)],
                ["Creators novos", "2"],
                ["Próxima ação", "Revisar creators, conceitos e roteiros juntos"],
              ].map(([name, value]) => (
                <div key={name} className="flex gap-4 border-b border-surface-border py-2">
                  <span className="min-w-36 text-on-surface-variant">{name}</span>
                  <span className="min-w-0 break-words font-medium text-primary">{value}</span>
                </div>
              ))}
              {error && (
                <p role="alert" className="mt-2 font-label-md text-label-md text-error">
                  {error}
                </p>
              )}
            </div>
          )}
        </Card>

        <div className="mt-6 flex items-center justify-between">
          <Button
            variant="ghost"
            onClick={() => (step === 0 ? navigate("/") : setStep((value) => value - 1))}
          >
            {step === 0 ? "Cancelar" : "Voltar"}
          </Button>
          {step < STEPS.length - 1 ? (
            <Button
              icon="arrow_forward"
              disabled={!canContinue}
              onClick={() => setStep((value) => value + 1)}
            >
              Continuar
            </Button>
          ) : (
            <Button icon="rocket_launch" loading={startRun.isPending} onClick={launch}>
              {startRun.isPending ? "Iniciando" : "Iniciar campanha"}
            </Button>
          )}
        </div>
      </div>
    </div>
  );
}
