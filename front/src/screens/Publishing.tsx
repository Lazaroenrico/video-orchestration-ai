import { PageHeader } from "../components/PageHeader";
import { Card, SectionTitle } from "../components/Card";
import { Icon } from "../components/Icon";
import { Button } from "../components/Button";

// Distribution/scheduling is explicitly out of scope for the engine (the approved
// terminal state is `assembled`). This screen renders the design faithfully as a
// non-interactive preview so the product surface is complete.
const DAYS = ["Sun", "Mon", "Tue", "Wed", "Thu", "Fri", "Sat"];
const START_OFFSET = 3; // month starts on a Wednesday, for layout only

export function Publishing() {
  const cells = Array.from({ length: 35 }, (_, i) => i - START_OFFSET + 1);
  return (
    <div>
      <PageHeader
        title="Publishing Calendar"
        subtitle="Schedule and manage content distribution."
        actions={
          <div className="flex items-center gap-2">
            <Button variant="ghost" icon="chevron_left">
              {""}
            </Button>
            <span className="font-headline-md text-headline-md text-primary">October 2024</span>
            <Button variant="ghost" icon="chevron_right">
              {""}
            </Button>
          </div>
        }
      />

      <div className="mb-4 rounded-lg bg-warning-review/10 text-warning-review px-4 py-2 font-label-md text-label-md inline-flex items-center gap-2">
        <Icon name="info" size={16} />
        Preview only — distribution is out of scope; the engine's terminal state is “assembled”.
      </div>

      <div className="grid grid-cols-12 gap-gutter">
        <div className="col-span-12 lg:col-span-3">
          <Card>
            <SectionTitle title="Ready to Schedule" />
            {[
              ["Morning Routine Campaign", "00:15"],
              ["Jacket Transition v2", "00:09"],
            ].map(([t, dur]) => (
              <div
                key={t}
                className="flex items-center gap-3 p-3 rounded-lg border border-surface-border mb-2 cursor-grab"
              >
                <div className="w-12 h-12 rounded bg-surface-container flex items-center justify-center text-on-surface-variant">
                  <Icon name="movie" size={18} />
                </div>
                <div className="flex-1 min-w-0">
                  <div className="font-body-md text-body-md text-primary truncate">{t}</div>
                  <div className="font-label-sm text-label-sm text-on-surface-variant">{dur}</div>
                </div>
              </div>
            ))}
          </Card>
        </div>

        <div className="col-span-12 lg:col-span-9">
          <Card padded={false} className="overflow-hidden">
            <div className="grid grid-cols-7 border-b border-surface-border">
              {DAYS.map((d) => (
                <div
                  key={d}
                  className="px-3 py-2 font-label-sm text-label-sm uppercase tracking-wider text-on-surface-variant text-center"
                >
                  {d}
                </div>
              ))}
            </div>
            <div className="grid grid-cols-7">
              {cells.map((day, i) => (
                <div
                  key={i}
                  className="min-h-[92px] border-b border-r border-surface-border p-2 last:border-r-0"
                >
                  {day > 0 && day <= 31 && (
                    <span className="font-label-sm text-label-sm text-on-surface-variant">{day}</span>
                  )}
                  {day === 2 && (
                    <div className="mt-1 rounded bg-ai-processing/10 text-ai-processing px-1.5 py-0.5 font-label-sm text-label-sm truncate">
                      Morning Routine
                    </div>
                  )}
                  {day === 5 && (
                    <div className="mt-1 rounded bg-success-published/10 text-success-published px-1.5 py-0.5 font-label-sm text-label-sm truncate">
                      Jacket v2
                    </div>
                  )}
                </div>
              ))}
            </div>
          </Card>
        </div>
      </div>
    </div>
  );
}
