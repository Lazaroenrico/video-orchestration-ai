import { PageHeader } from "../components/PageHeader";
import { Card } from "../components/Card";
import { Icon } from "../components/Icon";
import { Link } from "react-router";

export function Publishing() {
  return (
    <div>
      <PageHeader
        title="Distribution is not available in v1"
        subtitle="This workspace ends once a final video is assembled and ready for review."
      />

      <Card className="max-w-2xl">
        <div className="flex items-start gap-3">
          <div className="flex h-11 w-11 shrink-0 items-center justify-center rounded-lg bg-warning-review/10 text-warning-review">
            <Icon name="info" />
          </div>
          <div>
            <h2 className="font-headline-md text-headline-md text-primary">Keep the review surface honest</h2>
            <p className="mt-2 font-body-md text-body-md leading-relaxed text-on-surface-variant">
              Scheduling and channel publishing are intentionally outside this engine. Review final videos, inspect QC outcomes and export them through the infrastructure configured for your deployment.
            </p>
            <Link to="/review" className="mt-4 inline-flex min-h-11 items-center gap-2 whitespace-nowrap rounded-lg bg-primary px-4 font-label-md text-label-md font-bold text-on-primary">
              <Icon name="visibility" size={18} /> Review videos
            </Link>
          </div>
        </div>
      </Card>
    </div>
  );
}
