import { useState } from "react";
import { Route, Routes, useLocation } from "react-router-dom";
import { HomePage } from "./pages/HomePage";
import { GapRadarPage } from "./pages/GapRadarPage";
import { ComingSoonPage } from "./pages/ComingSoonPage";
import { ReliabilityPage } from "./pages/ReliabilityPage";
import { InvestigatePage } from "./pages/InvestigatePage";
import { InvestigationDetailPage } from "./pages/InvestigationDetailPage";
import { ScrapingIntro } from "./components/ScrapingIntro";

function App() {
  const location = useLocation();
  const [showIntro, setShowIntro] = useState(() => {
    if (location.pathname !== "/") return false;
    if (window.matchMedia?.("(max-width: 640px)").matches) return false;
    try {
      return sessionStorage.getItem("gapradar-intro-seen") !== "true";
    } catch {
      return true;
    }
  });

  function finishIntro() {
    try {
      sessionStorage.setItem("gapradar-intro-seen", "true");
    } catch {
      /* The visual intro does not require storage to work. */
    }
    setShowIntro(false);
  }

  return (
    <>
      {showIntro ? <ScrapingIntro onDone={finishIntro} /> : null}
      <Routes>
        <Route path="/" element={<HomePage />} />
        {/* The intelligence workspace. This is the product surface the
            landing narrative points at; it is unchanged, only re-homed. */}
        <Route path="/opportunities" element={<GapRadarPage />} />
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
