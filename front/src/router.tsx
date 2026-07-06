import { Routes, Route, Navigate } from "react-router-dom";
import { AppShell } from "./layout/AppShell";
import { Dashboard } from "./screens/Dashboard";
import { Campaigns } from "./screens/Campaigns";
import { CampaignDetail } from "./screens/CampaignDetail";
import { CreateWizard } from "./screens/CreateWizard";
import { Concepts } from "./screens/Concepts";
import { Creators } from "./screens/Creators";
import { Queue } from "./screens/Queue";
import { VideoReview } from "./screens/VideoReview";
import { Analytics } from "./screens/Analytics";
import { Publishing } from "./screens/Publishing";
import { Integrations } from "./screens/Integrations";
import { Settings } from "./screens/Settings";

// The Create Campaign wizard is a full-bleed flow in the Stitch design (no sidebar),
// so it lives outside the AppShell. Everything else renders inside the shell.
export function AppRoutes() {
  return (
    <Routes>
      <Route path="/campaigns/new" element={<CreateWizard />} />
      <Route element={<AppShell />}>
        <Route path="/" element={<Dashboard />} />
        <Route path="/campaigns" element={<Campaigns />} />
        <Route path="/campaigns/:runId" element={<CampaignDetail />} />
        <Route path="/scripts" element={<Concepts />} />
        <Route path="/creators" element={<Creators />} />
        <Route path="/queue" element={<Queue />} />
        <Route path="/review" element={<VideoReview />} />
        <Route path="/analytics" element={<Analytics />} />
        <Route path="/publishing" element={<Publishing />} />
        <Route path="/integrations" element={<Integrations />} />
        <Route path="/settings" element={<Settings />} />
      </Route>
      <Route path="*" element={<Navigate to="/" replace />} />
    </Routes>
  );
}
