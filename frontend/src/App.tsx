import { useState } from "react";
import { Route, Routes } from "react-router-dom";
import { GapRadarPage } from "./pages/GapRadarPage";
import { ComingSoonPage } from "./pages/ComingSoonPage";
import { ReliabilityPage } from "./pages/ReliabilityPage";
import { InvestigatePage } from "./pages/InvestigatePage";
import { InvestigationDetailPage } from "./pages/InvestigationDetailPage";
import { ScrapingIntro } from "./components/ScrapingIntro";

function App() {
  const [introDone, setIntroDone] = useState(false);

  return (
    <>
      <ScrapingIntro onDone={() => setIntroDone(true)} />
      <Routes>
        <Route
          path="/"
          element={<GapRadarPage introDone={introDone} />}
        />
        <Route
          path="/trends"
          element={
            <ComingSoonPage
              title="Trends"
              subtitle="Track how problem signals move over time."
            />
          }
        />
        <Route
          path="/saved"
          element={
            <ComingSoonPage
              title="Saved"
              subtitle="Bookmark problems you want to revisit."
            />
          }
        />
        <Route path="/reliability" element={<ReliabilityPage />} />
        <Route path="/investigate" element={<InvestigatePage />} />
        <Route
          path="/investigations/:investigationId"
          element={<InvestigationDetailPage />}
        />
      </Routes>
    </>
  );
}

export default App;
